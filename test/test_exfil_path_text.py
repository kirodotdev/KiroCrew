"""Encoded words in a URL path are not an exfiltration payload.

A browser percent-encodes a non-ASCII path as UTF-8, three octets per CJK
character, so a news headline in Chinese already forms the 20+ octet run the
heavy percent-encoding detector flags. These tests pin that such a path renders,
and that every shape an encoded secret could take still does not.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from kiro_crew.security import oauth_url_contains_credential, redact_exfiltration_urls

_HEADLINE = "港股-恒指低開196點-科技股承壓-聯想集團低開逾6-翰森逆市走高-015534459"


def _redacted(url: str) -> bool:
    result, _warnings = redact_exfiltration_urls(f"see {url}")
    return "[REDACTED" in result


@pytest.mark.parametrize(
    "url",
    [
        "https://hk.finance.yahoo.com/news/" + quote(_HEADLINE) + ".html?guccounter=1",
        "https://ja.wikipedia.org/wiki/" + quote("東京都の歴史と文化"),
        "https://ru.wikipedia.org/wiki/" + quote("Достопримечательности_Москвы"),
        "https://example.com/" + quote("《港股》恒指低開，科技股承壓"),
    ],
)
def test_encoded_words_in_path_are_kept(url: str) -> None:
    assert not _redacted(url)


@pytest.mark.parametrize(
    "path",
    [
        "%41" * 25,  # encoded ASCII
        quote("港股") + "%41" * 21,  # ASCII octets inside a run of words
        quote("ＡＫＩＡＩＯＳＦＯＤＮＮ７"),  # fullwidth letters and digits
        "%E2%80%8B" * 8,  # invisible format characters
        "%EE%80%80" * 8,  # private-use characters
        "%FF" * 25,  # not UTF-8
        "%E3%80%80" * 8,  # ideographic space, not a word
    ],
)
def test_other_encoded_runs_in_path_are_still_redacted(path: str) -> None:
    assert _redacted("https://example.com/" + path)


def test_encoded_words_in_query_are_still_redacted() -> None:
    assert _redacted("https://example.com/search?q=" + quote(_HEADLINE))


def test_oauth_admission_keeps_the_strict_floor() -> None:
    url = (
        "https://accounts.google.com/o/oauth2/v2/auth/"
        + quote(_HEADLINE)
        + "?client_id=abc&response_type=code"
    )
    assert oauth_url_contains_credential(url)
