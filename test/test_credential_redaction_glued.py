"""Two credentials written with nothing between them both redact whole.

A branch's body class can hold the leading characters of the next credential's
prefix, so before the fix the first match stopped short inside the second
credential and its remainder stayed in the text. Fixtures are synthetic: each is
a recognised prefix plus a repeated filler, never a real value.
"""

from __future__ import annotations

import base64
import json

import pytest

from kiro_crew.security import redact_credentials
from kiro_crew.security.redaction import _credential_matches

_TAG = "[REDACTED: credential]"


def _gh(filler: str) -> str:
    return "ghp_" + filler * 12


def _anthropic(filler: str) -> str:
    return "sk-ant-" + filler * 8


def _jwt() -> str:
    def seg(obj: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg({'sub': 'fixture'})}." + "s" * 43


GLUED = [
    pytest.param(_gh("Ab1"), _gh("Zq7"), id="same-rule"),
    pytest.param(_anthropic("Ab1"), _jwt(), id="key-then-jwt"),
    pytest.param(_gh("Ab1"), _anthropic("Zq7"), id="different-rules"),
]


@pytest.mark.parametrize(("first", "second"), GLUED)
def test_two_glued_credentials_both_redact(first: str, second: str) -> None:
    out, _ = redact_credentials("tokens: " + first + second + " end")
    assert out == f"tokens: {_TAG}{_TAG} end"


@pytest.mark.parametrize(("first", "second"), GLUED)
def test_each_credential_alone_or_spaced_is_unchanged(first: str, second: str) -> None:
    """The same values still redact one tag each when apart."""
    assert redact_credentials(first)[0] == _TAG
    assert redact_credentials(second)[0] == _TAG
    assert redact_credentials(first + " " + second)[0] == f"{_TAG} {_TAG}"


def test_three_glued_credentials_all_redact() -> None:
    text = _gh("Ab1") + _gh("Cd2") + _gh("Zq7")
    assert redact_credentials(f"[{text}]")[0] == f"[{_TAG}{_TAG}{_TAG}]"


def test_matches_keep_rising_starts_when_glued() -> None:
    """Callers walk the matches in order; a glued one starts after the one it extends."""
    text = "x " + _gh("Ab1") + _gh("Zq7") + " y " + _gh("Cd2")
    starts = [m.start() for m in _credential_matches(text, glued=True)]
    assert starts == sorted(set(starts))
    assert len(starts) == 3


def test_a_nested_match_does_not_extend_its_span() -> None:
    """A credential that ends inside the span before it changes nothing."""
    text = "Authorization: Bearer " + _jwt() + " tail"
    assert redact_credentials(text)[0] == f"{_TAG} tail"


@pytest.mark.timeout(60)
def test_scan_stays_linear_on_a_long_glued_run() -> None:
    """Every glued hit resumes past the one before it, so a long run is one pass;
    a scan that restarted inside each span would blow the timeout."""
    text = "".join(_gh(f"A{i % 10}b") for i in range(4000))
    assert redact_credentials(text)[0] == _TAG * 4000


def test_a_credential_glued_behind_a_nested_match_still_redacts() -> None:
    """A match nested inside the first one does not hide a third that runs past it."""
    jwt = _jwt()
    text = "sk-ant-" + "A" * 20 + "ghp_" + "B" * 36 + jwt + " end"
    out = redact_credentials(text)[0]
    assert jwt.split(".", 1)[1] not in out
    assert out.endswith(" end") and "B" * 10 not in out


def test_a_labelled_value_glued_after_a_token_redacts() -> None:
    """A field label whose separator the token before it did not take is a real
    field, so its value is redacted; the record names only the rule."""
    from kiro_crew.security.redaction import redact_credentials_with_records

    text = "ghp_" + "A" * 36 + "AccessKeyId=fixture-value-01 end"
    out, _, records = redact_credentials_with_records(text)
    assert "fixture-value-01" not in out and out.endswith(" end")
    assert all("A" * 10 not in r.value or r.rule == "github_token" for r in records)


def test_a_label_inside_a_value_that_took_its_separator_stays_part_of_it() -> None:
    """The text after a label the value already swallowed is not redacted as a field
    when it does not continue a token, matching the stream's Bearer discard."""
    text = "Authorization: Bearer " + "a-" * 30 + "AccessKeyId=" + ":visible done"
    assert redact_credentials(text)[0] == f"{_TAG}:visible done"


@pytest.mark.timeout(60)
def test_scan_stays_linear_on_many_labels_inside_one_match() -> None:
    """Labels inside a long value cost one bounded probe each until one extends the
    span; the rest are then inside an AWS value and skipped. The scan alone is timed:
    later passes have their own bounds."""
    text = "https://u:" + "aws_secret_access_key=v" * 200000 + "@host.example/p tail"
    assert len(list(_credential_matches(text, glued=True))) == 2


@pytest.mark.timeout(60)
def test_scan_stays_linear_on_tokens_each_followed_by_a_label() -> None:
    """A token whose body takes the next label's field name: the label is probed
    once and its value runs to the end, so the scan ends after one long read."""
    text = ("ghp_" + "a1" * 15 + "SecretAccessKey=") * 200000
    assert len(list(_credential_matches(text, glued=True))) == 2


@pytest.mark.timeout(60)
def test_scan_stays_linear_on_many_spans_followed_by_punctuation() -> None:
    """A probe that extends nothing reads only a bounded window past its span."""
    text = (_gh("Ab1") + ".") * 20000
    assert redact_credentials(text)[0] == (_TAG + ".") * 20000


def test_a_jwt_behind_many_nested_tokens_still_redacts() -> None:
    """More nested starts than the probe budget: the rest of the run is redacted."""
    jwt = _jwt()
    text = "sk-ant-" + "A" * 20 + _gh("Bc3") * 9 + jwt + " end"
    out = redact_credentials(text)[0]
    assert jwt.split(".", 1)[1] not in out and out.endswith(" end")


def test_a_glued_userinfo_link_redacts_through_its_last_at() -> None:
    """A link's userinfo is matched to the last `@` of its authority, as the plain
    scan does, even past the probe window."""
    filler = "p-" * 600
    text = _gh("Ab1") + "https://u:short@" + filler + "@host.example/p tail"
    out = redact_credentials(text)[0]
    assert filler[:40] not in out and out.endswith("host.example/p tail")


def test_a_glued_label_value_holding_a_link_is_not_cut_at_its_slash() -> None:
    """Only a URI start is matched again up to its authority's end; a label value
    that merely contains `://` keeps its whole greedy match."""
    text = "ghp_" + "A" * 36 + "AccessKeyId=fixture://value/rest-01 end"
    out = redact_credentials(text)[0]
    assert "rest-01" not in out and out.endswith(" end")


def test_a_glued_link_whose_at_lies_past_the_window_redacts() -> None:
    """A link start is probed even when no `@` falls inside the search window."""
    filler = "p-" * 600
    text = _gh("Ab1") + "https://u:" + filler + "@host.example/p tail"
    out = redact_credentials(text)[0]
    assert filler[:40] not in out and out.endswith("host.example/p tail")


@pytest.mark.timeout(60)
def test_scan_stays_linear_on_many_glued_links_without_userinfo() -> None:
    """Each link start reads only to its own authority's end."""
    text = (_gh("Ab1") + "https://host.example/p") * 50000
    assert len(list(_credential_matches(text, glued=True))) == 50000


def test_a_glued_pem_block_redacts_when_the_first_token_stops_at_its_space() -> None:
    """A PEM header holds a space, so a token ending on its `-----BEGIN` stops at
    that space; the header is still probed and the block redacted."""
    body = "\n".join(["Zm9vYmFyZml4dHVyZQ" + str(i) for i in range(4)])
    pem = "-----BEGIN " + "PRIVATE KEY-----\n" + body + "\n-----END " + "PRIVATE KEY-----"
    text = "sk-ant-" + "A" * 20 + pem + " tail"
    out = redact_credentials(text)[0]
    assert "Zm9vYmFy" not in out and out.endswith(" tail")


def test_a_glued_bearer_header_redacts_when_the_first_token_stops_at_its_space() -> None:
    value = "fixture-bearer-" + "x1" * 12
    text = _gh("Ab1") + "Authorization: Bearer " + value + " end"
    out = redact_credentials(text)[0]
    assert value not in out and out.endswith(" end")


def _pem(lines: list[str]) -> str:
    return (
        "-----BEGIN " + "PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END " + "PRIVATE KEY-----"
    )


def test_a_glued_bearer_header_after_a_labelled_value_redacts() -> None:
    """Only an AWS field name ending a value before a space is value text; a Bearer
    header there is a header of its own."""
    value = "fixture-bearer-" + "x1" * 8
    text = "SessionToken=xAuthorization: Bearer " + value + " end"
    out = redact_credentials(text)[0]
    assert value not in out and out.endswith(" end")


def test_a_glued_pem_block_past_the_probe_budget_redacts() -> None:
    """Spaced starts are still probed once the budget is spent."""
    body = ["Zm9vYmFyZml4dHVyZQ" + str(i) for i in range(4)]
    text = "sk-ant-" + "A" * 20 + _gh("Bc3") * 9 + _pem(body) + " tail"
    out = redact_credentials(text)[0]
    assert "Zm9vYmFy" not in out and "PRIVATE KEY" not in out and out.endswith(" tail")


def test_a_long_glued_pem_block_redacts_to_its_end_marker() -> None:
    """A PEM block longer than the probe window is matched to its END marker, not
    cut at a line break the window happens to land on."""
    body = ["QUFBQUFB"] + ["QUFB"] * 200
    text = "sk-ant-" + "A" * 20 + _pem(body) + " tail"
    out = redact_credentials(text)[0]
    assert "QUFB" not in out and out.endswith(" tail")


@pytest.mark.timeout(60)
def test_scan_stays_linear_on_many_glued_pem_headers() -> None:
    """Each PEM probe reads to the next END marker at most once."""
    block = "sk-ant-" + "A" * 20 + _pem(["Zm9vYmFy"]) + "."
    text = block * 20000
    assert len(list(_credential_matches(text, glued=True))) == 40000


@pytest.mark.timeout(60)
def test_scan_stays_linear_on_many_glued_pem_headers_with_no_end_marker() -> None:
    """The END-marker lookup is shared across the scan, so text with many glued
    headers and no END marker is searched for one once, not once per header."""
    block = "sk-ant-" + "A" * 20 + "-----BEGIN " + "PRIVATE KEY-----\n!\n"
    text = block * 80000
    assert len(list(_credential_matches(text, glued=True))) == 160000


_FIELD = "aws_secret_access_key=" + "Q" * 8


@pytest.mark.timeout(60)
def test_scan_stays_linear_on_many_field_labels_inside_a_link_password() -> None:
    """A field label inside an AWS value run already read to its end is value text
    of that run, so it is not probed again to the same end."""
    text = "https://u:" + _FIELD * 80000 + ",@host.example/p"
    out = redact_credentials(text)[0]
    assert "Q" * 8 not in out and out == _TAG + "host.example/p"


@pytest.mark.parametrize(
    "tail",
    [
        # A value run that stops at a delimiter: the field after it has its own value.
        ',aws_session_token="' + "S" * 24 + '"',
        "}" + "aws_session_token=" + "S" * 24,
        # A label whose own separator is the last character of the run reads past it.
        "aws_session_token=" + "S" * 24,
    ],
)
def test_a_field_after_a_cached_value_run_is_still_redacted(tail: str) -> None:
    """The cache covers only labels that end inside the run it read, so a field whose
    value lies past the run's end is still probed and redacted."""
    text = "https://u:" + _FIELD * 3 + tail + "@host.example/p ghp_" + "Ab1" * 12
    out = redact_credentials(text)[0]
    assert "S" * 24 not in out and "Q" * 8 not in out and "Ab1Ab1" not in out


@pytest.mark.timeout(60)
def test_scan_stays_linear_on_many_field_labels_inside_a_pem_block() -> None:
    """Starts inside a complete PEM block cannot reach past its END marker, so they
    are stepped over rather than each probed to the end of the block's body."""
    text = "-----BEGIN PRIVATE KEY-----\n" + _FIELD * 20000 + "\n-----END PRIVATE KEY-----x "
    out = redact_credentials(text)[0]
    assert "Q" * 8 not in out and out.endswith("x ")


@pytest.mark.timeout(60)
def test_scan_stays_linear_on_many_field_labels_inside_a_glued_pem_block() -> None:
    """The same holds once a glued PEM block extends the span of the match before it."""
    text = (
        "ghp_"
        + "Ab1" * 12
        + "-----BEGIN PRIVATE KEY-----\n"
        + _FIELD * 20000
        + "\n-----END PRIVATE KEY-----x "
    )
    out = redact_credentials(text)[0]
    assert "Q" * 8 not in out and "Ab1Ab1" not in out and out.endswith("x ")


def test_a_credential_glued_after_a_pem_block_still_redacts() -> None:
    """Only starts before the END marker are stepped over: a field or a Bearer
    header written right after the block is still probed and redacted."""
    pem = _pem(["Zm9vYmFy"])
    field = "aws_secret_access_key=Zz9" + "Kq7Rt2" * 6
    out = redact_credentials(pem + field + " tail")[0]
    assert "Kq7Rt2" not in out and out.endswith(" tail")
    bearer = "x Authorization: Bearer " + "Tk9" * 10
    out = redact_credentials(pem + bearer + " tail")[0]
    assert "Tk9Tk9" not in out and out.endswith(" tail")


def _long_jwt() -> str:
    def seg(obj: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    claims = {"sub": "fixture", "groups": [f"group-{i:04d}" for i in range(120)]}
    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg(claims)}." + "a-b_" * 9 + "abc"


def test_a_glued_jwt_whose_payload_runs_past_the_window_redacts() -> None:
    """A JWT start is probed on its own and read whole, however long its claims."""
    jwt = _long_jwt()
    text = "sk-ant-" + "A" * 20 + jwt + " end"
    out = redact_credentials(text)[0]
    assert jwt.split(".", 1)[1][:40] not in out and out.endswith(" end")


@pytest.mark.parametrize("sep", [" : ", "\t= ", " ="])
def test_a_spaced_aws_label_glued_after_a_token_redacts(sep: str) -> None:
    """A token's body that takes an AWS field name does not make that field value text;
    only an AWS value that swallowed the name does."""
    value = "fixture/session+value01"
    text = _gh("Ab1") + "SessionToken" + sep + value + " end"
    out = redact_credentials(text)[0]
    assert "fixture" not in out and out.endswith(" end")


@pytest.mark.timeout(60)
def test_scan_stays_linear_on_many_eyj_starts_inside_spans() -> None:
    """`eyJ` starts that are not JWTs cost one bounded probe each."""
    text = ("sk-ant-" + "A" * 20 + "eyJxyz" + ".") * 40000
    assert len(list(_credential_matches(text, glued=True))) == 40000


def test_a_large_jwt_with_many_inner_eyj_runs_keeps_the_text_after_it() -> None:
    """Only the first `eyJ` of a segment run is probed, so a large ordinary token does
    not spend the probe budget and the text glued after it stays readable."""
    header = base64.urlsafe_b64encode(b'{"alg": "HS256"}').rstrip(b"=").decode()
    payload = "eyJzdWIi" + "xeyJabc" * 30
    jwt = f"{header}.{payload}." + "s" * 43
    text = '{"access_token":"' + jwt + '","expires_in":300,"token_type":"Bearer"}'
    out = redact_credentials(text)[0]
    assert payload[:40] not in out
    assert out.endswith('","expires_in":300,"token_type":"Bearer"}')


@pytest.mark.parametrize("glue", [".", ","])
def test_a_credential_starting_inside_the_fail_closed_rest_still_redacts(glue: str) -> None:
    """The rest of a run redacted after the probe budget is spent is not skipped, so
    a credential that begins inside it is still matched whole."""
    value = "fixture-bearer-" + "x1" * 8
    text = "sk-ant-" + "A" * 20 + _gh("Bc3") * 8 + glue + "Authorization: Bearer " + value + " end "
    out = redact_credentials(text)[0]
    assert value not in out and out.endswith(" end ")


@pytest.mark.timeout(60)
def test_scan_stays_linear_on_many_exhausted_matches_in_one_run() -> None:
    """A fail-closed rest is read and redacted once per run, not once per match in it."""
    block = "sk-ant-" + "A" * 20 + _gh("Bc3") * 9 + "."
    text = block * 4000
    out = redact_credentials(text)[0]
    assert "Bc3" not in out


@pytest.mark.parametrize("label", ["SecretAccessKey = ", "aws_session_token : "])
def test_a_spaced_aws_label_past_the_probe_budget_redacts(label: str) -> None:
    """AWS field names are probed after the budget is spent, like PEM and Bearer
    starts, since their separator can sit after a space."""
    value = "fixtureSecretValueABCDEFGHIJklmnopqrstuv"
    text = "sk-ant-" + "A" * 20 + _gh("Bc3") * 8 + label + value + " end"
    out = redact_credentials(text)[0]
    assert value not in out and out.endswith(" end")


def test_a_spaced_aws_field_after_a_glued_header_redacts() -> None:
    """Whether the span's end is an AWS field's value is read from the match that
    extended it last, so a field after a glued Bearer header is probed."""
    secret = "abcdefFIXTUREsecretVALUEforTESTS01234567"
    text = (
        "SessionToken=xAuthorization: Bearer " + "t-" * 20 + "SecretAccessKey = " + secret + " end"
    )
    out = redact_credentials(text)[0]
    assert secret not in out and out.endswith(" end")


@pytest.mark.parametrize(
    "label",
    ["Authorization:" + " " * 520 + "Bearer ", "SecretAccessKey" + " " * 520 + "= "],
)
def test_a_glued_label_whose_value_lies_past_a_long_whitespace_run_redacts(label: str) -> None:
    """A field label is read whole before the probe window is applied to its value."""
    value = "fixtureLongGapValueABCDEFGHIJklmnopqrst"
    text = _gh("Ab1") + label + value + " end"
    out = redact_credentials(text)[0]
    assert value not in out and out.endswith(" end")


@pytest.mark.timeout(60)
def test_scan_stays_linear_on_many_glued_labels_before_long_whitespace() -> None:
    block = _gh("Ab1") + "Authorization:" + " " * 600 + "x "
    text = block * 2000
    assert len(list(_credential_matches(text, glued=True))) == 2000


@pytest.mark.parametrize(
    "glue",
    [
        "Authorization: Bearer YYYYYYYYYYYYaws_secret_access_key=",
        '"Authorization": "Bearer abcaws_session_token=',
    ],
)
def test_an_aws_field_glued_to_a_bearer_token_redacts(glue: str) -> None:
    """A Bearer token's `=` padding can take an AWS field's separator; the field is
    still probed, since only an AWS field's own value holds a field name as text."""
    value = "fixtureBearerGlueValueABCDEFGHIJklmnopq"
    out = redact_credentials(glue + value + " rest")[0]
    assert value[:12] not in out and out.endswith(" rest")


@pytest.mark.timeout(60)
def test_scan_stays_linear_on_eyj_starts_sharing_one_segment_run() -> None:
    """Every `eyJ` in one segment run is walked once across the whole scan, not once
    per match that starts inside the run."""
    text = (_gh("Ab1")[:37] + "eyJx_") * 16000
    assert len(list(_credential_matches(text, glued=True))) >= 16000


@pytest.mark.parametrize("label", ["aws_secret_access_key", "Authorization"])
def test_a_field_name_starting_just_before_the_window_bound_redacts(label: str) -> None:
    """A field name that starts just before a probe window's bound is matched whole,
    since the start search reads past the bound by the longest start literal. Every
    filler length near the window size is tried, so one of them puts the name's start
    in the last characters before the bound."""
    value = "fixtureBoundaryValueABCDEFGHIJklmnopqrs"
    sep = "= " if label.startswith("aws") else ": Bearer "
    for filler in range(440, 520):
        text = "Authorization: Bearer " + "A" * filler + label + " " * 600 + sep + value + " end"
        out = redact_credentials(text)[0]
        assert value[:12] not in out, filler
        assert out.endswith(" end"), filler


def test_the_start_lookahead_covers_every_start_literal() -> None:
    """The lookahead past a search bound is at least as long as every literal start."""
    from kiro_crew.security import redaction_patterns as rp

    literals: list[str] = []
    for pattern in (rp._SPACED_START_RE.pattern, rp._URI_START_RE.pattern):
        flat = pattern.replace("(?i:", "").replace("(?:", "").replace(")", "")
        flat = flat.replace("?", "").replace("\\+", "+")
        literals.extend(flat.split("|"))
    longest = max(len(lit) for lit in literals)
    assert longest >= len("aws_secret_access_key")
    assert rp._START_LOOKAHEAD >= longest


_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_SESSION_VALUE = "FQoGZXIvYXdzEJr" + "Kq7Rt2" * 8


@pytest.mark.parametrize("prefix", ["", "ghp_" + "a" * 36])
@pytest.mark.parametrize("separator", ["= ", "=\n", ": ", "=  "])
def test_a_field_whose_separator_a_value_took_before_a_space_redacts(
    prefix: str, separator: str
) -> None:
    """A field name and separator glued into an AWS value, with the field's own value
    after whitespace, is probed: its value is redacted, not left as text."""
    text = prefix + "aws_secret_access_key=" + _AWS_SECRET + "aws_session_token" + separator
    out = redact_credentials(text + _SESSION_VALUE + " tail")[0]
    assert "Kq7Rt2" not in out and "EXAMPLEKEY" not in out and out.endswith(" tail")


def test_a_field_name_ending_a_value_before_its_separator_stays_value_text() -> None:
    """A field name that ends an AWS value before a space, with its separator after
    the space, is still part of that value: the text after the separator is kept."""
    out = redact_credentials("SessionToken=" + "a-" * 30 + "AccessKeyId : visible text")[0]
    assert out.endswith(" : visible text")
