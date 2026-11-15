"""The exfil URL scan sees the whole URL, not only the part a renderer links.

``_URL_RE`` ends a URL at ``)``, ``'`` or ``>``. Those bytes are legal inside a
URL, so a payload placed after one was never scanned, and an uppercase scheme
was not matched at all. These tests pin each bypass as caught by the blocking
scan (``scan_exfiltration_urls``), and pin that the URL redactor, which masks
only a credential VALUE inside a URL, never replaces a URL whole and never eats
the text after a link's closer.

Sample URLs are built from parts so no literal long-query URL appears here.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from redaction_structured_cases import github_classic

from kiro_crew.security import exfil, redact_exfiltration_urls, scan_exfiltration_urls

# 56 base64 characters: past the 40-char blob bar on its own.
_BLOB = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9w"
_EVIL = "evil.example.com"
# Assembled so no literal key id sits in source.
_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
# A GitHub token with a valid checksum, which the credential pass masks.
_TOKEN = github_classic(seed=7)
_TAG = "[REDACTED: credential]"
_EXEMPT = "docs.example.com"


@pytest.fixture
def exempt_host(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The companion's exact-host exemption, which only the blocking scan reads."""
    monkeypatch.setattr(exfil, "_exempt_exact_hosts", lambda: frozenset({_EXEMPT}))
    yield _EXEMPT


@pytest.mark.parametrize("stop", [")", "'", ">"])
def test_payload_after_a_stop_byte_in_the_path_is_caught(stop: str) -> None:
    text = f"https://{_EVIL}/a{stop}b?data={_BLOB}"
    assert scan_exfiltration_urls(text)
    # A blob that is no credential is not masked, and the URL is never replaced whole.
    assert redact_exfiltration_urls(text) == (text, [])


def test_path_credential_after_a_paren_is_caught() -> None:
    text = f"https://{_EVIL}/a){_KEY_ID}"
    assert scan_exfiltration_urls(text)


def test_uppercase_scheme_is_scanned() -> None:
    text = f"HTTPS://{_EVIL}/a?data={_BLOB}"
    assert scan_exfiltration_urls(text)


def test_payload_after_a_closing_single_quote_is_caught() -> None:
    text = f"'https://{_EVIL}/a?x=1',{_BLOB}"
    assert scan_exfiltration_urls(text)


def test_payload_right_after_a_closing_paren_is_caught() -> None:
    text = f"(https://{_EVIL}/a?x=1){_BLOB} tail"
    assert scan_exfiltration_urls(text)


def test_a_credential_in_a_wrapped_link_is_masked_and_the_text_after_kept() -> None:
    """Only the value is masked: the URL around it and the text after its closer stay."""
    link = f"[x](https://{_EVIL}/?ref={_TOKEN}) after"
    assert redact_exfiltration_urls(link)[0] == f"[x](https://{_EVIL}/?ref={_TAG}) after"
    html = f"<a href='https://{_EVIL}/a?token={_TOKEN}'>label</a> tail"
    assert redact_exfiltration_urls(html)[0] == (
        f"<a href='https://{_EVIL}/a?token={_TAG}'>label</a> tail"
    )
    angle = f"<https://{_EVIL}/?token={_TOKEN}>text after"
    assert redact_exfiltration_urls(angle)[0] == f"<https://{_EVIL}/?token={_TAG}>text after"


def test_a_credential_outside_every_url_is_left_to_the_credential_pass() -> None:
    text = f"token {_TOKEN} and https://{_EVIL}/a?x=1"
    assert redact_exfiltration_urls(text) == (text, [])


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


def test_glued_url_after_a_paren_is_judged_under_its_own_host(exempt_host: str) -> None:
    """The exempt host's relaxed checks never reach the glued second URL."""
    text = f"(https://{exempt_host}/a)https://{_EVIL}/?d={_BLOB}"
    assert scan_exfiltration_urls(text)


@pytest.mark.parametrize("space", ["\u00a0", "\u3000", "\u2003"])
def test_unicode_space_ends_the_url(space: str, exempt_host: str) -> None:
    """A second URL after a Unicode space is judged under its own host."""
    text = f"https://{exempt_host}/a{space}https://{_EVIL}/?d={_BLOB}"
    assert scan_exfiltration_urls(text)


def test_a_flagged_link_before_a_paren_is_recorded_whole() -> None:
    """The blocked-link record names the address a markdown link renders, without
    the paren that closes it, and an allowed host lets it render live."""
    from kiro_crew.security.blocked_links import blocked_links

    host = "reviews.example.com"
    link = f"https://{host}/x?q=" + "a" * 250
    reply = f"[x]({link}) done"
    assert [r["url"] for r in blocked_links(reply)] == [link]
    assert blocked_links(reply, allowed_hosts=frozenset({host})) == []


def test_cjk_prose_glued_after_a_link_is_kept() -> None:
    prose = "请参阅文档中的说明" * 30
    text = f"请参阅[文档](https://docs.python.org/3/search.html?q=x){prose}\n下一行"
    assert scan_exfiltration_urls(text) == []
    assert redact_exfiltration_urls(text)[0] == text


def test_payload_glued_between_cjk_prose_is_still_caught() -> None:
    text = f"[文档](https://{_EVIL}/?q=x)请看{_BLOB}这里"
    assert scan_exfiltration_urls(text)


@pytest.mark.parametrize("scheme", ["HTTPS", "https", "HtTpS"])
def test_glued_url_inside_an_exempt_link_is_judged_under_its_own_host(
    scheme: str, exempt_host: str
) -> None:
    """The exempt outer host's relaxed checks cover only its own span."""
    outer = f"{scheme}://{exempt_host}/r?x=1`[open]("
    text = f"{outer}https://{_EVIL}/?data={_BLOB})"
    assert scan_exfiltration_urls(text)


def test_embedded_url_past_a_chain_of_exempt_ones_is_caught(exempt_host: str) -> None:
    text = f"https://{exempt_host}/r?n=https://{exempt_host}/x?m=https://{_EVIL}/?d={_BLOB}"
    assert scan_exfiltration_urls(text)


def test_clean_url_inside_an_exempt_link_is_left_alone(exempt_host: str) -> None:
    text = f"https://{exempt_host}/r?next=https://{_EVIL}/page?id=7"
    assert scan_exfiltration_urls(text) == []
    assert redact_exfiltration_urls(text) == (text, [])


def test_embedded_url_after_a_bare_host_decoy_is_caught(exempt_host: str) -> None:
    """A clean embedded URL that ends early does not end the embedded walk."""
    text = (
        f"`HTTPS://{exempt_host}/r?x=1`https://decoy.example.com[open]"
        f"(https://{_EVIL}/?data={_BLOB})"
    )
    assert scan_exfiltration_urls(text)


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
    # The URL is never replaced whole: at most a credential value in it is masked.
    head = text.split("X-Amz-Signature=")[0]
    assert redact_exfiltration_urls(text)[0].startswith(head)


def test_payload_glued_after_a_presigned_link_is_still_caught() -> None:
    text = f"[f]({_PRESIGNED})?data={_BLOB}"
    assert scan_exfiltration_urls(text)


def test_slack_app_link_in_markdown_bold_is_left_alone() -> None:
    from kiro_crew import slack_manifest

    text = f"**[create]({slack_manifest.deep_link('kirocrew')})**"
    assert scan_exfiltration_urls(text) == []
    assert redact_exfiltration_urls(text)[0] == text
