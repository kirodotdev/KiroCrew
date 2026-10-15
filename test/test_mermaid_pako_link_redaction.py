"""Mermaid pako links survive only the bare-secret pass and only after validation."""

from __future__ import annotations

import base64
import hashlib
import json
import time
import zlib

import pytest

from kiro_crew.security import StreamRedactor, redact_credentials
from kiro_crew.security import redaction as _redaction

KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
SEGMENT_KEY = "wJalrXUtnFEMIzK7MDENGqbPxRfiCYEXAMPLEKEY"
TAG = "[REDACTED: credential]"
PREFIX = "https://mermaid.live/edit#pako:"


def _encode_state(state: bytes) -> str:
    return base64.urlsafe_b64encode(zlib.compress(state, 9)).decode().rstrip("=")


def _payload(code: str) -> str:
    state = json.dumps({"code": code}, separators=(",", ":")).encode()
    return _encode_state(state)


def _url(code: str) -> str:
    return PREFIX + _payload(code)


def _raw_state_url(state: str) -> str:
    return PREFIX + _encode_state(state.encode())


def _near_cap_url() -> str:
    raw = b"".join(hashlib.sha256(i.to_bytes(4, "big")).digest() for i in range(2_200))
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    code = " ".join(encoded[i : i + 20] for i in range(0, len(encoded), 20))[:64_700]
    payload = _payload(code)
    assert _redaction._MERMAID_PAKO_PAYLOAD_CAP - 128 < len(payload)
    assert len(payload) <= _redaction._MERMAID_PAKO_PAYLOAD_CAP
    return PREFIX + payload


def _stream(text: str, width: int) -> str:
    redactor = StreamRedactor()
    return (
        "".join(redactor.feed(text[i : i + width]) for i in range(0, len(text), width))
        + redactor.flush()
    )


def _key_shaped_url(code: str) -> str:
    for salt in range(2_000):
        candidate = _url(f"{code}\n%% {salt}")
        if _redaction._text_contains_bare_secret(candidate):
            return candidate
    raise AssertionError("fixture did not produce a pass-3 candidate")


def test_valid_pass_3_candidate_survives_batch_and_streams_byte_for_byte() -> None:
    code = "flowchart TD\n" + "\n".join(f"  A{i} --> B{i}: label {i}" for i in range(300))
    url = _key_shaped_url(code)

    assert len(url) > 512
    assert _redaction._text_contains_bare_secret(url)
    assert redact_credentials(url) == (url, [])
    for width in (1, 7, 31, 511):
        assert _stream(url, width) == url


def test_terminating_slash_cannot_split_a_secret_across_pako_state() -> None:
    slash = KEY.index("/")
    redactor = StreamRedactor()

    first = redactor.feed(PREFIX + KEY[:slash])
    second = redactor.feed(KEY[slash:] + " done")
    output = first + second + redactor.flush()

    assert KEY not in output
    assert output == PREFIX + TAG + " done"


def test_near_cap_one_byte_stream_stays_within_cpu_budget() -> None:
    url = _near_cap_url()
    redactor = StreamRedactor()

    started = time.thread_time()
    parts = [redactor.feed(char) for char in url]
    parts.append(redactor.flush())
    elapsed = time.thread_time() - started

    assert "".join(parts) == url
    assert elapsed < 2.0  # flake-ok: thread CPU bounds work, not scheduler delay


@pytest.mark.parametrize(
    "rewrite",
    [
        lambda url: url.replace("https://", "http://", 1),
        lambda url: url.replace("mermaid.live", "MERMAID.LIVE", 1),
        lambda url: url.replace("/edit#", "/view#", 1),
        lambda url: url.replace("mermaid.live", "mermaid.live:443", 1),
        lambda url: url + "=",
        lambda url: url + "/suffix",
    ],
)
def test_route_and_encoding_variants_earn_no_exemption(rewrite) -> None:
    assert _redaction._document_link_spans(rewrite(_url("flowchart TD\n  A --> B"))) == []


@pytest.mark.parametrize("payload", ["A" * 80, "eJw", "____", "abcdA"])
def test_invalid_payload_earns_no_exemption(payload: str) -> None:
    assert _redaction._document_link_spans(PREFIX + payload) == []


@pytest.mark.parametrize(
    "state",
    [
        r'{"code":"flowchart TD","aws\u005faccess\u005fkey\u005fid":"opaque"}',
        r'{"code":"flowchart TD","Authoriz\u0061tion":"Bearer opaque-token"}',
    ],
)
def test_escaped_json_credential_composition_earns_no_exemption(state: str) -> None:
    assert _redaction._document_link_spans(_raw_state_url(state)) == []


def test_decompression_bomb_earns_no_exemption() -> None:
    url = _url("flowchart TD\n  A[" + "x" * (_redaction._MERMAID_PAKO_SOURCE_CAP * 4) + "]")

    assert _redaction._document_link_spans(url) == []


@pytest.mark.parametrize(
    "code",
    [
        "flowchart TD\n  A[AKIAIOSFODNN7EXAMPLE]",
        "flowchart TD\n  A["
        + base64.b64encode(f"aws_secret_access_key={KEY}".encode()).decode()
        + "]",
        f"flowchart TD\n  A[{SEGMENT_KEY}]",
        "flowchart TD\n  A[https://example.com/hook?token=Zq81xLmP0vR7tYc2]",
    ],
)
def test_every_standard_credential_pass_scans_decoded_source(code: str) -> None:
    assert _redaction._document_link_spans(_url(code)) == []


def test_decode_failure_does_not_change_fixed_or_higher_confidence_passes() -> None:
    text = f"AKIAIOSFODNN7EXAMPLE {PREFIX}{'A' * 80} ?token=opaque-value"
    redacted, warnings = redact_credentials(text)

    assert redacted.startswith(f"{TAG} {PREFIX}")
    assert redacted.endswith(f"?token={TAG}")
    assert len(warnings) >= 2


def test_multiple_links_report_exact_input_spans() -> None:
    first = _url("flowchart TD\n  A --> B")
    second = _url("sequenceDiagram\n  Alice->>Bob: hello")
    text = f"before {first} middle {second} after"

    assert _redaction._document_link_spans(text) == [
        (text.index(first), text.index(first) + len(first)),
        (text.index(second), text.index(second) + len(second)),
    ]


def test_top_level_pako_links_share_one_decoded_byte_budget() -> None:
    first = _url("flowchart TD\n  A[" + "x" * 33_000 + "]")
    second = _url("flowchart TD\n  B[" + "y" * 33_000 + "]")
    text = first + " " + second

    assert _redaction._document_link_spans(first) == [(0, len(first))]
    assert _redaction._document_link_spans(second) == [(0, len(second))]
    assert _redaction._document_link_spans(text) == [(0, len(first))]


def test_records_stay_explainable_for_a_rejected_decoded_secret() -> None:
    unsafe = _key_shaped_url(f"flowchart TD\n  A[{SEGMENT_KEY}]")
    redacted, warnings, records = _redaction.redact_credentials_with_records(
        f"before {unsafe} after"
    )

    assert warnings
    assert unsafe not in redacted
    assert records
    assert all(record.rule == "bare_aws_secret" for record in records)
    assert [record.ordinal for record in records] == list(range(len(records)))


def test_valid_link_creates_no_redaction_record() -> None:
    url = _url("flowchart TD\n  A --> B")

    assert _redaction.redact_credentials_with_records(url) == (url, [], [])
