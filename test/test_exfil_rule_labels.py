"""The suspicious-URL placeholder names the rule that removed the link.

``[REDACTED: suspicious URL to <host> (<reason>)]``: the reason comes from a
fixed table keyed by rule id, so a reader can tell a link that looked like it
carried a secret from one that only tripped a shape heuristic, and the words
can never carry anything the URL chose.

Every sample URL is assembled from parts so this file's own text never holds
one the redactor would rewrite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiro_crew.security.exfil import (
    EXFIL_RULE_LABELS,
    EXFILTRATION_REDACTION_TAG_PREFIX,
    exfiltration_redaction_tag,
    redact_exfiltration_urls,
    redact_exfiltration_urls_with_records,
    restore_allowed_links,
)

_HOST = "collect.example.com"
_AWS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
_GITHUB_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def _url(query: str, host: str = _HOST) -> str:
    return "https://" + host + "/p?" + query


#: One URL per rule, each tripping exactly that rule first.
_RULE_SAMPLES = {
    "exfil_hard_credential": _url("k=" + _AWS_KEY_ID),
    "exfil_fixed_credential": _url("k=" + _GITHUB_TOKEN),
    "exfil_encoded_credential": _url("k=" + "".join(f"%{ord(c):02X}" for c in _AWS_KEY_ID)),
    "exfil_decode_saturated": _url("k=%25252525" + "41"),
    "exfil_percent_encoding": _url("k=" + "%41" * 25),
    "exfil_query_length": _url("q=" + "word-" * 50),
    "exfil_query_pattern": _url("d=" + "Zm9v" * 11),
}


def _redact_one(url: str) -> tuple[str, str]:
    cleaned, _warnings, records = redact_exfiltration_urls_with_records("see " + url + " now")
    assert len(records) == 1, "fixture no longer trips the redactor; pick another URL"
    return cleaned, records[0]["rule"]


@pytest.mark.parametrize(
    ("rule", "label"),
    [
        ("exfil_hard_credential", "credential"),
        ("exfil_fixed_credential", "credential"),
        ("exfil_encoded_credential", "encoded credential"),
        ("exfil_decode_saturated", "heavy encoding, may be legitimate"),
        ("exfil_percent_encoding", "heavy encoding, may be legitimate"),
        ("exfil_query_length", "long query, may be legitimate"),
        ("exfil_query_pattern", "random-looking query, may be legitimate"),
    ],
)
def test_each_rule_names_its_reason_in_the_placeholder(rule: str, label: str) -> None:
    cleaned, got_rule = _redact_one(_RULE_SAMPLES[rule])
    assert got_rule == rule
    tag = f"[REDACTED: suspicious URL to {_HOST} ({label})]"
    assert cleaned == f"see {tag} now"
    assert exfiltration_redaction_tag(_HOST, rule) == tag


@pytest.mark.parametrize("rule", sorted(_RULE_SAMPLES))
def test_the_prefix_stays_byte_identical(rule: str) -> None:
    cleaned, _ = _redact_one(_RULE_SAMPLES[rule])
    assert cleaned.count(EXFILTRATION_REDACTION_TAG_PREFIX) == 1
    assert EXFILTRATION_REDACTION_TAG_PREFIX == "[REDACTED: suspicious URL to "


@pytest.mark.parametrize(
    ("rule", "first", "second"),
    [
        ("exfil_query_length", "q=" + "a" * 210, "z=" + "0123456789" * 25),
        ("exfil_query_pattern", "d=" + "Q" * 44, "blob=" + "xYz9" * 15),
        ("exfil_hard_credential", "k=" + _AWS_KEY_ID, "id=" + "AKIA" + "Z" * 16),
    ],
)
def test_same_rule_with_different_content_gives_the_same_text(
    rule: str, first: str, second: str
) -> None:
    """Nothing from the URL beyond its host reaches the placeholder."""
    one, rule_one = _redact_one(_url(first))
    two, rule_two = _redact_one(_url(second))
    assert rule_one == rule_two == rule
    assert one == two


def test_an_unknown_or_missing_rule_adds_no_reason() -> None:
    assert exfiltration_redaction_tag(_HOST, None) == f"[REDACTED: suspicious URL to {_HOST}]"
    assert exfiltration_redaction_tag(_HOST, "exfil_new_rule") == (
        f"[REDACTED: suspicious URL to {_HOST}]"
    )


def test_every_traced_rule_has_a_reason() -> None:
    """A rule added to ``_exfil_url_warning`` without a reason is caught here."""
    source = (
        Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "security" / "exfil.py"
    ).read_text("utf-8")
    traced = set(re.findall(r'trace\("(exfil_[a-z_]+)"\)', source))
    assert traced, "trace() call shape changed; update this test"
    assert traced <= set(EXFIL_RULE_LABELS)


def test_the_dashboard_matcher_accepts_every_reason() -> None:
    """``RedactionCards.tsx`` matches the reason from the same fixed words."""
    frontend = (
        Path(__file__).resolve().parents[1]
        / "website"
        / "src"
        / "components"
        / "RedactionCards.tsx"
    ).read_text("utf-8")
    found = re.search(r"const LINK_REASON = /\(\?: \\\(\(\?:([a-z ,|-]+)\)", frontend)
    assert found, "LINK_REASON shape changed; update this test"
    # Superset, not equality: the words are append-only (stored messages carry them).
    assert set(EXFIL_RULE_LABELS.values()) <= set(found.group(1).split("|"))


def test_an_allowed_host_still_restores_a_labelled_placeholder() -> None:
    url = _RULE_SAMPLES["exfil_query_length"]
    cleaned, _warnings, records = redact_exfiltration_urls_with_records("see " + url)
    assert "(long query, may be legitimate)]" in cleaned
    restored, left = restore_allowed_links(cleaned, records, frozenset({_HOST}))
    assert restored == "see " + url
    assert left == []


def test_a_credential_placeholder_is_never_restored() -> None:
    url = _RULE_SAMPLES["exfil_hard_credential"]
    cleaned, _warnings, records = redact_exfiltration_urls_with_records("see " + url)
    restored, left = restore_allowed_links(cleaned, records, frozenset({_HOST}))
    assert restored == cleaned
    assert _AWS_KEY_ID not in restored
    assert left == records


def test_redaction_is_a_fixed_point_with_the_reason() -> None:
    once, _ = redact_exfiltration_urls("see " + _RULE_SAMPLES["exfil_query_pattern"])
    twice, warnings = redact_exfiltration_urls(once)
    assert twice == once
    assert warnings == []


def test_a_bracketed_ipv6_host_is_restored() -> None:
    url = "http://[fd00::1]/p?q=" + "word-" * 50
    cleaned, _warnings, records = redact_exfiltration_urls_with_records("see " + url)
    assert "[fd00::1] (long query, may be legitimate)]" in cleaned
    restored, left = restore_allowed_links(cleaned, records, frozenset({"[fd00::1]"}))
    assert restored == "see " + url
    assert left == []


def test_a_segment_pairs_the_raw_record_when_the_reasons_differ() -> None:
    """A URL cut across stream deltas can trip another rule than the whole URL does."""
    from types import SimpleNamespace

    from kiro_crew.dashboard.chat_runner import _redact_segment

    url = _RULE_SAMPLES["exfil_query_length"]
    streamed = "see " + exfiltration_redaction_tag(_HOST, "exfil_percent_encoding")
    slot = SimpleNamespace(segment_raw_text="see " + url, credential_evidence=[])
    text, links, _ = _redact_segment(slot, streamed)
    assert text == streamed
    assert [r["rule"] for r in links] == ["exfil_query_length"]


def test_a_placeholder_with_an_unknown_reason_is_not_restored() -> None:
    """Only the table's words pair with a record; echoed prose never becomes a link."""
    url = _RULE_SAMPLES["exfil_query_length"]
    _cleaned, _warnings, records = redact_exfiltration_urls_with_records("see " + url)
    forged = f"see {EXFILTRATION_REDACTION_TAG_PREFIX}{_HOST} (see below)]"
    restored, left = restore_allowed_links(forged, records, frozenset({_HOST}))
    assert restored == forged
    assert left == records
