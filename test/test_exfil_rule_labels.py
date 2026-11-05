"""The suspicious-URL placeholder names the rule that removed the link.

``[REDACTED: suspicious URL to <host>] (<reason>)``: the reason comes from a
fixed table keyed by rule id, so a reader can tell a link that looked like it
carried a secret from one that only tripped a shape heuristic, and the words
can never carry anything the URL chose. The tag keeps its old shape, and a
reader that needs the reason compares the whole fixed suffix.

Every sample URL is assembled from parts so this file's own text never holds
one the redactor would rewrite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiro_crew.security.exfil import (
    EXFIL_RULE_LABELS,
    EXFIL_RULE_REASON_SUFFIXES,
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
    tag = f"[REDACTED: suspicious URL to {_HOST}] ({label})"
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


@pytest.mark.parametrize("rule", sorted(EXFIL_RULE_LABELS))
def test_the_tag_keeps_its_old_shape_and_the_reason_follows_it(rule: str) -> None:
    """Every reader of the tag sees main's ``<prefix><host>]``; the reason is an exact suffix."""
    tag = exfiltration_redaction_tag(_HOST, rule)
    head = f"{EXFILTRATION_REDACTION_TAG_PREFIX}{_HOST}]"
    assert tag.startswith(head)
    assert tag[len(head) :] in EXFIL_RULE_REASON_SUFFIXES


def test_an_allowed_host_still_restores_a_labelled_placeholder() -> None:
    url = _RULE_SAMPLES["exfil_query_length"]
    cleaned, _warnings, records = redact_exfiltration_urls_with_records("see " + url)
    assert "] (long query, may be legitimate)" in cleaned
    restored, left = restore_allowed_links(cleaned, records, frozenset({_HOST}))
    assert restored == "see " + url
    assert left == []


def test_a_restored_link_drops_only_its_own_reason() -> None:
    """The record's reason is removed whole; any other text after the tag stays."""
    url = _RULE_SAMPLES["exfil_query_length"]
    cleaned, _warnings, records = redact_exfiltration_urls_with_records("see " + url + " now")
    restored, _ = restore_allowed_links(cleaned, records, frozenset({_HOST}))
    assert restored == "see " + url + " now"
    other = cleaned.replace("] (long query, may be legitimate)", "] (credential)")
    restored, _ = restore_allowed_links(other, records, frozenset({_HOST}))
    assert restored == "see " + url + " (credential) now"


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


@pytest.mark.parametrize("host", [_HOST, "[fd00::1]"])
def test_a_segment_keeps_its_record_when_a_delta_cut_changes_the_reason(host: str) -> None:
    """A URL cut across stream deltas can trip another rule than the whole URL does."""
    from kiro_crew.dashboard import chat_runner

    head = f"https://{host}/p?q=" + "word-" * 50
    deltas = ["see ", head, f"&k={_AWS_KEY_ID} now"]
    slot = chat_runner._ChatSlot("s1")
    streamed = ""
    for delta in deltas:
        safe, removed = chat_runner._redact_delta(delta)
        chat_runner._accumulate_segment_raw(slot, delta, safe, removed)
        streamed += safe
    assert exfiltration_redaction_tag(host, "exfil_query_length") in streamed
    text, links, _ = chat_runner._redact_segment(slot, streamed)
    assert text == f"see {exfiltration_redaction_tag(host, 'exfil_hard_credential')} now"
    assert [(r["domain"], r["rule"], r["path"]) for r in links] == [
        (host, "exfil_hard_credential", "/p")
    ]


def test_reason_words_are_not_a_removed_value_kept_by_the_segment() -> None:
    """A delta removed ``h``; the whole-text reason has more ``h`` than the streamed one."""
    from kiro_crew.dashboard import chat_runner

    deltas = [
        "https://first.example.com/?token=h",
        "opaque-value now; ",
        f"https://{_HOST}/p?q=" + "word-" * 50,
        "%41" * 25,
    ]
    slot = chat_runner._ChatSlot("s1")
    streamed = ""
    for delta in deltas:
        safe, removed = chat_runner._redact_delta(delta)
        chat_runner._accumulate_segment_raw(slot, delta, safe, removed)
        streamed += safe
    assert "h" in slot.segment_removed
    assert exfiltration_redaction_tag(_HOST, "exfil_query_length") in streamed
    text, _links, _ = chat_runner._redact_segment(slot, streamed)
    assert text != chat_runner._SEGMENT_UNCHECKED_TEXT
    assert text.endswith(exfiltration_redaction_tag(_HOST, "exfil_percent_encoding"))
    assert "opaque-value" not in text


def test_the_reason_suffixes_are_append_only() -> None:
    """Stored messages carry these exact suffixes; a reworded reason adds one, never replaces."""
    assert set(EXFIL_RULE_REASON_SUFFIXES) >= {
        " (credential)",
        " (encoded credential)",
        " (heavy encoding, may be legitimate)",
        " (long query, may be legitimate)",
        " (random-looking query, may be legitimate)",
    }


def test_text_inside_the_tag_is_still_not_a_placeholder() -> None:
    """Words inside the brackets were never a reason: main's tag shape decides."""
    url = _RULE_SAMPLES["exfil_query_length"]
    _cleaned, _warnings, records = redact_exfiltration_urls_with_records("see " + url)
    forged = f"see {EXFILTRATION_REDACTION_TAG_PREFIX}{_HOST} (long query, may be legitimate)]"
    restored, left = restore_allowed_links(forged, records, frozenset({_HOST}))
    assert restored == forged
    assert left == records
