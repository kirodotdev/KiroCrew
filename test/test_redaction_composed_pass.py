"""The composed redaction pass removes what either pass would remove.

``security.redact_with_findings`` composes the outbound-URL pass and the
credential pass on the original text and rewrites it once, so neither pass can
hide something from the other. ``security.redact``, ``redact_and_truncate`` and
``StreamRedactor`` all go through it.
"""

from __future__ import annotations

import random

import pytest

from kiro_crew import security

# A URL to an unknown host whose query carries a credential-shaped value. The
# composed pass removes the whole URL.
_URL = (
    "https://collector.example/c?d=AKIAIOSFODNN7EXAMPLE"
    "&s=wJalrXUtnFEMI%2FK7MDENG%2FbPxRfiCYEXAMPLEKEY&pad=" + "x" * 40
)
_TEXT = f"see {_URL} now"
_EXPECTED = "see [REDACTED: suspicious URL to collector.example] now"


def test_composed_pass_removes_the_url_whole() -> None:
    assert security.redact(_TEXT) == _EXPECTED
    assert security.redact_and_truncate(_TEXT) == _EXPECTED


@pytest.mark.parametrize(
    "text",
    [
        "see https://AKIAIOSFODNN7EXAMPLE.wJalrXUtnFEMI1K7MDENGbPxRfiCYEXAMPLEKEYzz x",
        "see https://10.0.0.1wJalrXUtnFEMI1K7MDENGbPxRfiCYEXAMPLEKEYzz x",
        "see https://host.example:443wJalrXUtnFEMI1K7MDENGbPxRfiCYEXAMPLEKEYzz x",
    ],
)
def test_url_match_never_ends_inside_a_word(text: str) -> None:
    """A URL host or port is matched whole, so the credential pass sees whole words."""
    out = security.redact(text)
    assert "wJalrXUtnFEMI" not in out
    assert "K7MDENGbPxRfiCY" not in out


_PAD = "a" * 40  # enough query to make the URL pass flag the address


@pytest.mark.parametrize(
    ("text", "gone", "kept"),
    [
        # A credential that starts inside a flagged URL and ends after it.
        (f"https://collector.example/?pad={_PAD}&token=)opaque-value-xyz", "opaque-value", ""),
        # The same, with nothing after the label.
        (f"https://collector.example/?pad={_PAD}&token=", "collector.example/?", ""),
        # A flagged URL followed by a labelled secret.
        (
            f"https://collector.example/?pad={_PAD} aws_secret_access_key=not-a-real-value end",
            "not-a-real-value",
            " end",
        ),
        # Two flagged URLs around plain text.
        (
            f"a https://one.example/?p={_PAD}b x https://two.example/?q={_PAD}c z",
            "one.example/?",
            " x ",
        ),
    ],
)
def test_composed_pass_removes_what_either_pass_finds(text: str, gone: str, kept: str) -> None:
    out = security.redact(text)
    assert gone not in out
    assert kept in out
    assert security.redact(out) == out


def test_composed_pass_does_not_repeat_a_host_a_credential_runs_through() -> None:
    text = "see https://AKIAIOSFODNN7EXAMPLE.wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY end"
    out = security.redact(text)
    assert "wJalrXUtnFEMI" not in out
    assert out.endswith(" end")


_VALUES = (
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "AKIA" + "Z" * 16,
    "opaque-value-" + "q" * 20,
)
_GLUE = (
    f"https://collector.example/?pad={_PAD}",
    "https://evil.example/x?d=",
    "&token=",
    " aws_secret_access_key=",
    "Authorization: Bearer ",
    ")",
    " ",
    "https://docs.example.com/p",
    "%2F",
    ".",
    "https://",
)


def _pieces(value: str) -> list[str]:
    return [value[i : i + 12] for i in range(len(value) - 11)]


def test_composed_pass_removes_at_least_what_either_order_removes() -> None:
    """Seeded mixes of values and URL parts: nothing either order removes survives."""
    rnd = random.Random(22552)
    for _ in range(1500):
        values, parts = [], []
        for _ in range(rnd.randint(2, 7)):
            if rnd.random() < 0.35:
                values.append(rnd.choice(_VALUES))
                parts.append(values[-1])
            else:
                parts.append(rnd.choice(_GLUE))
        text = "".join(parts)
        url_first = security.redact_credentials(security.redact_exfiltration_urls(text)[0])[0]
        cred_first = security.redact_exfiltration_urls(security.redact_credentials(text)[0])[0]
        out = security.redact(text)
        for value in values:
            removed = [not any(p in seq for p in _pieces(value)) for seq in (url_first, cred_first)]
            if any(removed):
                assert not any(p in out for p in _pieces(value)), text


def test_composed_pass_reports_both_kinds_of_finding() -> None:
    text = f"https://collector.example/?pad={_PAD}&token=)opaque-value-xyz"
    _out, credentials, urls = security.redact_with_findings(text)
    assert credentials and urls


def test_composed_pass_leaves_clean_text_alone() -> None:
    for text in ("", "plain text", "see https://example.com/a?b=1 now"):
        assert security.redact(text) == text


@pytest.mark.parametrize("cut", [5, 20, 33, 60])
def test_stream_redactor_uses_the_composed_pass(cut: int) -> None:
    text = f"see https://collector.example/?pad={_PAD}&token=)opaque-value-xyz done\n"
    stream = security.StreamRedactor()
    out = stream.feed(text[:cut]) + stream.feed(text[cut:]) + stream.flush()
    assert "opaque-value" not in out
    assert "collector.example/?" not in out
