"""The exfil base64 heuristic treats ``=`` as trailing padding, not a joiner.

With ``=`` inside the character class, ``name=`` plus a short value fused into
one 40-char run, so an ordinary ``trainingId=<32-char id>`` link was redacted.
These tests pin both directions: that link shape now renders, and every payload
shape the heuristic exists for (padded base64, AWS key ids, heavy percent
encoding, a 40-hex value, a payload in the parameter NAME) is still redacted.

Sample URLs are built from parts so no literal long-query URL appears here.
"""

from __future__ import annotations

import base64
import re

from kiro_crew.security import redact_exfiltration_urls, scan_exfiltration_urls
from kiro_crew.security.exfil import _EXFIL_PATTERNS

# The spelling this change replaced, kept only to show which fixtures it
# matched; nothing in the product uses it.
_OLD_B64_RE = re.compile(r"[A-Za-z0-9+/=]{40,}")

# Invented, shape-matched course id: 32 alphanumerics, under the 40-char bar.
_COURSE_ID = "COURSE2026041420115089b6a4269f01"


def _url(query: str) -> str:
    return "https://learn.example.com/t/view?" + query


def _redacted(url: str) -> bool:
    result, warnings = redact_exfiltration_urls(f"see {url} for details")
    return bool(warnings) and url not in result


def test_name_equals_short_id_link_is_kept() -> None:
    query = "trainingId=" + _COURSE_ID + "&lms=LEARN"
    assert len(_COURSE_ID) == 32
    # The old class fused the key, the "=" and the id into one 40-char run.
    assert _OLD_B64_RE.search(query)
    assert not _EXFIL_PATTERNS.search(query)
    url = _url(query)
    text = f"see {url} for details"
    result, warnings = redact_exfiltration_urls(text)
    assert warnings == []
    assert result == text
    assert scan_exfiltration_urls(text) == []


def test_forty_char_base64_value_is_redacted() -> None:
    assert _redacted(_url("lms=LEARN&id=" + "Ab3/" * 10))


def test_double_padded_base64_secret_is_redacted() -> None:
    blob = base64.b64encode(b"S" * 34).decode()
    assert blob.endswith("==") and len(blob) == 48
    match = _EXFIL_PATTERNS.search("d=" + blob)
    assert match is not None and match.group(0) == blob
    assert _redacted(_url("d=" + blob + "&lms=LEARN"))


def test_minimum_length_padded_base64_is_redacted() -> None:
    """Padding counts toward the 40 chars, as it did in the old class."""
    for size, pad in ((28, "=="), (29, "=")):
        blob = base64.b64encode(b"S" * size).decode()
        assert len(blob) == 40 and blob.endswith(pad)
        assert _OLD_B64_RE.search(blob)
        match = _EXFIL_PATTERNS.search("d=" + blob)
        assert match is not None and match.group(0) == blob
        assert _redacted(_url("d=" + blob + "&lms=LEARN"))


def test_aws_key_shaped_value_is_redacted() -> None:
    assert _redacted(_url("k=" + "AKIA" + "IOSFODNN7EXAMPLE"))


def test_heavy_percent_encoding_is_redacted() -> None:
    assert _redacted(_url("q=" + "%41" * 25))


def test_forty_hex_value_is_redacted() -> None:
    # A 40-hex value is still a 40-char run; narrowing it is out of scope here.
    assert _redacted(_url("sha=" + "0123456789abcdef" * 2 + "01234567"))


def test_payload_in_parameter_name_is_redacted() -> None:
    assert _redacted(_url("Ab3x" * 10 + "=1"))


def test_split_payload_behaviour_is_unchanged_by_the_separator() -> None:
    """Sub-40 chunks never formed a run across ``&``, so ``=`` adds no channel.

    This documents behaviour that predates the change: the old class also saw no
    run in the ``&``-split form. The ``=``-split form is now equivalent to it,
    and the aggregate length signal bounds either one.
    """
    half = "Ab3x" * 5  # 20 chars
    amp_split = f"a={half}&b={half}"
    eq_split = f"a={half}={half}"
    assert not _OLD_B64_RE.search(amp_split)
    assert not _EXFIL_PATTERNS.search(amp_split)
    assert not _EXFIL_PATTERNS.search(eq_split)
    assert not _redacted(_url(amp_split))
    assert not _redacted(_url(eq_split))

    long_split = "=".join(["Ab3x" * 7 + "Ab"] * 7)  # 7 x 30 chars
    assert not _EXFIL_PATTERNS.search(long_split)
    assert _redacted(_url("a=" + long_split))
