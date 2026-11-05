"""The exfil URL scan sees the whole URL, not only the part a renderer links.

``_URL_RE`` ends a URL at ``)``, ``'`` or ``>``. Those bytes are legal inside a
URL, so a payload placed after one was never scanned, and an uppercase scheme
was not matched at all. These tests pin each bypass as caught, and pin that a
flagged link is still replaced without eating the text after it.

Sample URLs are built from parts so no literal long-query URL appears here.
"""

from __future__ import annotations

import pytest

from kiro_crew.security import redact_exfiltration_urls, scan_exfiltration_urls
from kiro_crew.security.exfil import (
    EXFILTRATION_REDACTION_TAG_PREFIX,
    redact_exfiltration_urls_with_records,
    restore_allowed_links,
)

# 56 base64 characters: past the 40-char blob bar on its own.
_BLOB = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9w"
_EVIL = "evil.example.com"
_TAG = f"{EXFILTRATION_REDACTION_TAG_PREFIX}{_EVIL}]"
# Assembled so no literal key id sits in source.
_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.mark.parametrize("stop", [")", "'", ">"])
def test_payload_after_a_stop_byte_in_the_path_is_caught(stop: str) -> None:
    text = f"https://{_EVIL}/a{stop}b?data={_BLOB}"
    assert scan_exfiltration_urls(text)
    cleaned, _ = redact_exfiltration_urls(text)
    assert _BLOB not in cleaned and _TAG in cleaned


def test_path_credential_after_a_paren_is_caught() -> None:
    text = f"https://{_EVIL}/a){_KEY_ID}"
    assert scan_exfiltration_urls(text)
    assert _KEY_ID not in redact_exfiltration_urls(text)[0]


def test_uppercase_scheme_is_scanned() -> None:
    text = f"HTTPS://{_EVIL}/a?data={_BLOB}"
    assert scan_exfiltration_urls(text)
    assert redact_exfiltration_urls(text)[0] == _TAG


def test_payload_after_a_closing_single_quote_is_caught() -> None:
    text = f"'https://{_EVIL}/a?x=1',{_BLOB}"
    assert scan_exfiltration_urls(text)
    assert redact_exfiltration_urls(text)[0] == f"'{_TAG}"


def test_payload_right_after_a_closing_paren_is_caught() -> None:
    text = f"(https://{_EVIL}/a?x=1){_BLOB} tail"
    assert scan_exfiltration_urls(text)
    assert redact_exfiltration_urls(text)[0] == f"({_TAG} tail"


def test_flagged_link_keeps_the_text_after_its_closer() -> None:
    """The URL itself is flagged, so only the URL is replaced."""
    text = f"[x](https://{_EVIL}/?d={_BLOB}) after"
    assert redact_exfiltration_urls(text)[0] == f"[x]({_TAG}) after"
    html = f"<a href='https://{_EVIL}/a?data={_BLOB}'>label</a> tail"
    assert redact_exfiltration_urls(html)[0] == f"<a href='{_TAG}'>label</a> tail"
    angle = f"<https://{_EVIL}/?d={_BLOB}>text after"
    assert redact_exfiltration_urls(angle)[0] == f"<{_TAG}>text after"


@pytest.mark.parametrize(
    "text",
    [
        "(https://docs.example.com/a?x=1).",
        "see 'https://docs.example.com/a?x=1', then",
        "[x](https://docs.example.com/wiki/Foo_(bar)) done",
        "<a href='https://docs.example.com/a'>label</a>",
    ],
)
def test_clean_link_in_wrappers_is_left_alone(text: str) -> None:
    assert scan_exfiltration_urls(text) == []
    assert redact_exfiltration_urls(text)[0] == text


def test_glued_url_after_a_paren_is_judged_under_its_own_host() -> None:
    """The exempt host's relaxed checks never reach the glued second URL."""
    exempt = "docs.example.com"
    text = f"(https://{exempt}/a)https://{_EVIL}/?d={_BLOB}"
    cleaned, warnings = redact_exfiltration_urls(text, exempt_hosts=frozenset({exempt}))
    assert warnings and _BLOB not in cleaned
    assert cleaned == f"(https://{exempt}/a){_TAG}"


@pytest.mark.parametrize("space", ["\u00a0", "\u3000", "\u2003"])
def test_unicode_space_ends_the_url(space: str) -> None:
    """A second URL after a Unicode space is judged under its own host."""
    exempt = "docs.example.com"
    text = f"https://{exempt}/a{space}https://{_EVIL}/?d={_BLOB}"
    cleaned, warnings = redact_exfiltration_urls(text, exempt_hosts=frozenset({exempt}))
    assert warnings and cleaned == f"https://{exempt}/a{space}{_TAG}"


def test_cjk_prose_glued_after_a_link_is_kept() -> None:
    prose = "请参阅文档中的说明" * 30
    text = f"请参阅[文档](https://docs.python.org/3/search.html?q=x){prose}\n下一行"
    assert scan_exfiltration_urls(text) == []
    assert redact_exfiltration_urls(text)[0] == text


def test_payload_glued_between_cjk_prose_is_still_caught() -> None:
    text = f"[文档](https://{_EVIL}/?q=x)请看{_BLOB}这里"
    assert scan_exfiltration_urls(text)
    assert _BLOB not in redact_exfiltration_urls(text)[0]


@pytest.mark.parametrize("scheme", ["HTTPS", "https", "HtTpS"])
def test_glued_url_inside_an_exempt_link_is_judged_under_its_own_host(scheme: str) -> None:
    """The exempt outer host's relaxed checks cover only its own span."""
    exempt = "docs.example.com"
    outer = f"{scheme}://{exempt}/r?x=1`[open]("
    text = f"{outer}https://{_EVIL}/?data={_BLOB})"
    cleaned, warnings = redact_exfiltration_urls(text, exempt_hosts=frozenset({exempt}))
    assert warnings and cleaned == f"{outer}{_TAG})"
    _, _, records = redact_exfiltration_urls_with_records(text, exempt_hosts=frozenset({exempt}))
    assert [r["domain"] for r in records] == [_EVIL]


def test_embedded_url_past_a_chain_of_exempt_ones_is_caught() -> None:
    exempt = "docs.example.com"
    text = f"https://{exempt}/r?n=https://{exempt}/x?m=https://{_EVIL}/?d={_BLOB}"
    cleaned, warnings = redact_exfiltration_urls(text, exempt_hosts=frozenset({exempt}))
    assert warnings and _BLOB not in cleaned and _TAG in cleaned


def test_clean_url_inside_an_exempt_link_is_left_alone() -> None:
    exempt = "docs.example.com"
    text = f"https://{exempt}/r?next=https://{_EVIL}/page?id=7"
    assert redact_exfiltration_urls(text, exempt_hosts=frozenset({exempt})) == (text, [])


def test_allowed_link_before_a_paren_is_restored() -> None:
    host = "reviews.example.com"
    link = f"https://{host}/x?q=" + "a" * 250
    reply = f"[x]({link}) done"
    text, _, records = redact_exfiltration_urls_with_records(reply)
    assert [r["url"] for r in records] == [link]
    assert restore_allowed_links(text, records, frozenset({host})) == (reply, [])


def test_embedded_url_after_a_bare_host_decoy_is_caught() -> None:
    """A clean embedded URL that ends early does not end the embedded walk."""
    exempt = "docs.example.com"
    text = f"`HTTPS://{exempt}/r?x=1`https://decoy.example.com[open](https://{_EVIL}/?data={_BLOB})"
    cleaned, warnings = redact_exfiltration_urls(text, exempt_hosts=frozenset({exempt}))
    assert warnings and _BLOB not in cleaned and _TAG in cleaned
    assert "https://decoy.example.com[open](" in cleaned


# Assembled so no literal presigned URL sits in source; each value passes its validator.
_PRESIGNED = (
    "https://my-bucket.s3.us-east-1.amazonaws.com/report.csv?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    f"&X-Amz-Credential={_KEY_ID}%2F20261008%2Fus-east-1%2Fs3%2Faws4_request"
    "&X-Amz-Date=20261008T000000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=" + "b" * 64
)


@pytest.mark.parametrize(
    "text",
    [
        f"[f]({_PRESIGNED})",
        f"**[f]({_PRESIGNED})**",
        f"|[f]({_PRESIGNED})|",
        f"[f]({_PRESIGNED})。",
    ],
)
def test_presigned_link_in_markdown_is_left_alone(text: str) -> None:
    """Bytes glued after a valid presigned link do not undo its exemption."""
    assert scan_exfiltration_urls(text) == []
    assert redact_exfiltration_urls(text)[0] == text


def test_payload_glued_after_a_presigned_link_is_still_caught() -> None:
    text = f"[f]({_PRESIGNED})?data={_BLOB}"
    cleaned, warnings = redact_exfiltration_urls(text)
    assert warnings and _BLOB not in cleaned


def test_slack_app_link_in_markdown_bold_is_left_alone() -> None:
    from kiro_crew import slack_manifest

    text = f"**[create]({slack_manifest.deep_link('kirocrew')})**"
    assert scan_exfiltration_urls(text) == []
    assert redact_exfiltration_urls(text)[0] == text
